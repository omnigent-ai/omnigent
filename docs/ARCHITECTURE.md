# Architecture

This is a high-level map of how omnigent's pieces fit together, aimed
at new contributors who'd like to find their way around before opening
their first PR. It's intentionally short. Deep dives belong in the
per-subsystem docs (`POLICIES.md`, `AGENT_YAML_SPEC.md`, the SDK README,
etc.).

> **Status:** scaffold. Sections marked `[maintainer fill]` are stubs
> waiting for ground-truth wording from someone with the full design
> context. The shape is meant to be edited freely.

## 30-second pitch

omnigent is a runtime for declarative agents. You write an agent as a
short YAML file (a prompt, an executor, an optional list of sub-agents
the supervisor can delegate to) and omnigent runs it across terminal,
web, native app, mobile, and a REST API. The bundled `debby` example
fans out to a Claude and a GPT sub-agent in parallel; `polly` is a
multi-agent coding orchestrator.

## The four roles

```
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   Client (UI)    │ ─► │     Server       │ ◄─ │     Runner       │
│ CLI / web / app  │SSE │  /v1/sessions    │WS  │ executes agent   │
│ mobile / SDK     │    │  /v1/responses*  │    │ talks to LLMs    │
└──────────────────┘    └──────────────────┘    └──────────────────┘
                                ▲
                                │ HTTPS
                        ┌───────┴─────────┐
                        │  Harness (LLM)  │
                        │ claude / codex  │
                        │ openai-agents…  │
                        └─────────────────┘
*legacy, being phased out in favor of sessions-first
```

- **Client.** UI surfaces that talk to the server. Terminal REPL, web
  UI (`ap-web/`), native desktop wrapper, mobile, and the Python SDK
  (`sdks/python-client/`).
- **Server** (`omnigent/server/`). Holds session state, brokers
  events between client and runner over SSE, runs guardrails and
  policy enforcement, exposes the REST + SSE surface (see
  `openapi.json`).
- **Runner** (`omnigent/runner/`). Executes the agent on the
  user's machine (or a sandbox). Owns the harness subprocess and
  the local filesystem.
- **Harness.** The actual LLM integration: Claude Code, Codex, Pi,
  the OpenAI Agents SDK, the Open Responses protocol, or custom
  ones declared via the `executor.harness` field in an agent's
  YAML. See `omnigent/runtime/harnesses/`.

`omnigent/cli.py` is the glue that starts the server + runner pair
locally and connects the REPL to it. `omni run AGENT` is the canonical
entry point.

## Repository layout

| Path | What's there |
| --- | --- |
| `omnigent/` | The Python package. CLI, server, runner, harnesses. |
| `omnigent/cli.py` | The `omni` command surface. It's 9KLOC today; see issue #147 for the gradual decomposition tracking. |
| `omnigent/server/` | FastAPI app, routes, schemas, websocket tunnel. |
| `omnigent/runner/` | Local agent execution, policy evaluation, tool dispatch. |
| `omnigent/runtime/` | Harness adapters and the shared scaffold. |
| `omnigent/llms/` | Multi-provider LLM client (OpenAI Responses-shaped). |
| `sdks/python-client/` | `omnigent_client` package - HTTP/SSE client for headless use. |
| `sdks/ui/` | `omnigent_ui_sdk` - shared terminal-rendering helpers. |
| `ap-web/` | Vue SPA for the web UI. Independent build chain. |
| `examples/` | `debby` and `polly` bundled reference agents. |
| `deploy/` | Per-target deploy configs (modal, fly, railway, render, daytona, docker, hf-spaces). |
| `tests/` | Pytest suite. Split into unit / integration / e2e / runner / server / inner / frontends. |
| `docs/` | Spec docs (`AGENT_YAML_SPEC.md`, `POLICIES.md`, and this file). |
| `openapi.json` | Generated OpenAPI spec, committed for drift detection. |

## Request flow: an agent turn

```
1. User types question in REPL / web UI / mobile
2. Client POSTs to /v1/sessions/{id}/events with the user message
3. Server validates against agent guardrails (POLICIES.md §...)
4. Server emits SSE: SessionStatusEvent (queued -> in_progress)
5. Runner picks up the turn via WS tunnel
6. Runner invokes the harness with prompt + tool schemas
7. Harness streams LLM events back to runner
8. Runner emits ServerStreamEvent SSE for each delta
9. Client renders (and observability hooks, if attached, fire)
10. Turn ends with CompletedEvent / FailedEvent / IncompleteEvent / CancelledEvent
```

The SDK's `BlockStream` consumes the SSE stream and emits semantic
"blocks" (text, reasoning, tool call, tool result). The SDK's
`StreamHooks` dataclass exposes per-lifecycle callbacks that third-
party tools (tracers, metrics collectors) can register against.

## Two extension surfaces worth knowing

- **Adding a harness.** Create a new module under
  `omnigent/runtime/harnesses/<your-harness>/`. Register the
  executor type. Implement the streaming protocol. `[maintainer
  fill: pointer to a worked harness PR or template]`.
- **Adding a tool.** Tools come in three flavors: MCP, Python
  function tools (decorated with `@omnigent_client.tool`), and
  sub-agent delegations. The agent's YAML declares which it uses.
  See `AGENT_YAML_SPEC.md` for the schema.

## Observability surfaces

- **`StreamHooks` callbacks.** Client-side. Fires per lifecycle
  event (response start/end, tool call start/end, reasoning start/end,
  message start/end, compaction start/end, retry, server error,
  elicitation). Sessions-first path picked up hook support in PR #43.
  Adapter example: [omnigent-mlflow-quickstart](https://github.com/debu-sinha/omnigent-mlflow-quickstart).
- **Server logs.** `~/.omnigent/logs/server/` for server-process
  logs, `~/.omnigent/logs/host-daemon/` for the host daemon.
- **`SubAgentSummary` read model.** Surfaced via
  `/v1/sessions/{id}/sub-agents` for REPL / web UI. See issue
  #146 for the open question on whether sub-agent lifecycle
  should also surface as an event.

## What to read next

| If you're working on… | Start here |
| --- | --- |
| The CLI | `omnigent/cli.py`, `CONTRIBUTING.md` |
| The server | `omnigent/server/app.py`, `openapi.json`, `omnigent/server/routes/sessions.py` |
| A harness adapter | `omnigent/runtime/harnesses/_scaffold.py` |
| Agent YAML | `docs/AGENT_YAML_SPEC.md` |
| Policies / guardrails | `docs/POLICIES.md` |
| Web UI | `ap-web/README.md` (if present, else `ap-web/package.json`) |
| The Python SDK | `sdks/python-client/omnigent_client/__init__.py`, `sdks/python-client/README.md` |
| Deployment | `deploy/README.md` and the per-target subdirectories |

## Open questions for maintainers

- `[maintainer fill]` Canonical name for the host-daemon vs runner
  distinction. The code uses both; readers get confused.
- `[maintainer fill]` Pointer to the design doc for the sessions-
  first migration (the legacy `/v1/responses` story).
- `[maintainer fill]` Where do ADRs (architectural decision records)
  live, or is this file the closest thing today?
